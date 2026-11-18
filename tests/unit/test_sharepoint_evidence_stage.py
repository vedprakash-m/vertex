"""Synthetic legacy-journal tests, not configured-store integration tests."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.commands.gather_pipeline import evidence_extraction_stage
from src.commands.gather_pipeline.sharepoint_evidence_stage import (
    _build_workstream_keyword_map,
    _derive_risk_level,
    _deserialize_signal,
    _load_ado_risk_levels,
    _parse_eta_from_milestone,
    _route_signal_to_lane,
    run_sharepoint_evidence_stage,
)
from src.core.evidence_models import SourceRef, WorkstreamEvidence
from src.core.evidence_store import load_approved_evidence_by_lane, load_evidence_records
from src.core.models import Confidence, RiskLevel


_AS_OF = datetime(2026, 6, 18, 12, tzinfo=timezone.utc)


def _signal(
    signal_id: str,
    text: str,
    *,
    source: str = "lt_deck",
    review_policy: str = "approved",
    workstream_id: str | None = None,
) -> dict[str, object]:
    return {
        "id": signal_id,
        "timestamp": "2026-06-17T10:00:00Z",
        "source": source,
        "program_id": "acme",
        "workstream_id": workstream_id,
        "entity_refs": [],
        "text": text,
        "raw_ref": None,
        "confidence": "high",
        "review_policy": review_policy,
        "metadata": {
            "doc_title": "Status deck",
            "doc_url": "https://example.test/deck",
            "cadence_days": 14,
        },
    }


def _write_signals(root: Path, *signals: dict[str, object]) -> None:
    path = root / "acme" / "signals" / "journal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(signal) + "\n" for signal in signals),
        encoding="utf-8",
    )


def test_missing_signal_journal_produces_no_evidence(tmp_path: Path) -> None:
    result = run_sharepoint_evidence_stage(program_id="acme", programs_root=tmp_path, as_of=_AS_OF)

    assert (result.lanes_updated, result.evidence_written, result.gaps) == (0, 0, ())
    assert not (tmp_path / "acme" / "journal" / "evidence_store.jsonl").exists()


def test_legacy_uppercase_risk_in_evidence_is_canonical(tmp_path: Path) -> None:
    path = tmp_path / "acme" / "journal" / "evidence_store.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"lane_id": "acme.delivery", "risk_level": "UNKNOWN"}) + "\n", encoding="utf-8")

    assert _load_ado_risk_levels(tmp_path, "acme")["acme.delivery"] is RiskLevel.UNKNOWN


def test_legacy_uppercase_confidence_in_signal_is_canonical() -> None:
    record = _signal("legacy", "Historical synthetic signal")
    record["confidence"] = "NONE"

    assert _deserialize_signal(record).confidence is Confidence.NONE


def test_only_approved_sharepoint_sources_are_processed(tmp_path: Path) -> None:
    _write_signals(
        tmp_path,
        _signal("pending", "Risk: pending", review_policy="pending"),
        _signal("dismissed", "Risk: dismissed", review_policy="dismissed"),
        _signal("other-source", "Risk: other", source="workiq"),
    )

    result = run_sharepoint_evidence_stage(program_id="acme", programs_root=tmp_path, as_of=_AS_OF)

    assert (result.lanes_updated, result.evidence_written, result.gaps) == (0, 0, ())
    assert load_evidence_records("acme", programs_root=tmp_path) == ()


def test_approved_markers_route_persist_and_gate_with_backing_signals(tmp_path: Path) -> None:
    baseline = WorkstreamEvidence(
        lane_id="acme.delivery",
        synthesized_at=_AS_OF,
        risk_level=RiskLevel.LOW,
        etas=(),
        blocking_items=(),
        owners=(),
        source_refs=(
            SourceRef(
                "workiq_email", "Prior status", date(2026, 6, 16), None,
                canonical_id="synthetic-native-id",
            ),
        ),
        raw_excerpts=(),
        confidence=0.8,
        narrative_summary="Prior status",
    )
    assert evidence_extraction_stage.persist_evidence(baseline, program_id="acme", programs_root=tmp_path)
    _write_signals(
        tmp_path,
        _signal("risk", "Risk: Deployment blocker owner:ava"),
        _signal("milestone", "Milestone: Deployment launch, 2026-06-30 owner:ben"),
        _signal("decision", "Decision: Deployment rollout approved", review_policy="auto_approved"),
        _signal("metric", "Metric: Deployment success 94%", source="sharepoint"),
        _signal("pending", "Risk: Deployment pending", review_policy="pending"),
        _signal("umbrella", "Decision: General planning"),
    )
    workstreams = (SimpleNamespace(id="acme.delivery", name="Delivery", aliases=("Deployment",)),)

    result = run_sharepoint_evidence_stage(
        program_id="acme",
        programs_root=tmp_path,
        as_of=_AS_OF,
        workstreams=workstreams,
    )

    assert (result.lanes_updated, result.evidence_written, result.gaps) == (2, 2, ())
    records = load_evidence_records("acme", programs_root=tmp_path)
    assert len(records) == 3
    delivery = records[1]
    evidence = delivery.evidence
    assert evidence.lane_id == "acme.delivery"
    assert evidence.risk_level == RiskLevel.HIGH
    assert evidence.lt_deck_alignment == "diverged"
    assert evidence.blocking_items == ("Risk: Deployment blocker owner:ava",)
    assert evidence.owners == ("ava", "ben")
    assert len(evidence.etas) == 1
    assert evidence.etas[0].eta_date == date(2026, 6, 30)
    assert evidence.etas[0].owner == "ben"
    assert evidence.confidence == 0.9
    assert evidence.stale_after == _AS_OF + timedelta(days=14)
    assert evidence.source_refs[0].source_type == "lt_deck"
    assert evidence.source_refs[-1].source_type == "sharepoint_docx"
    assert evidence.source_refs[0].source_date == date(2026, 6, 17)
    assert evidence.source_refs[0].permalink == "https://example.test/deck"
    assert delivery.backing_signal_ids == ("risk", "milestone", "decision", "metric")
    assert records[2].evidence.lane_id == "acme"
    assert records[2].backing_signal_ids == ("umbrella",)

    approved = load_approved_evidence_by_lane(
        "acme",
        programs_root=tmp_path,
        approved_signal_ids={"risk", "milestone", "decision", "metric", "umbrella"},
    )
    assert approved["acme.delivery"].risk_level == RiskLevel.HIGH
    not_fully_approved = load_approved_evidence_by_lane(
        "acme", programs_root=tmp_path, approved_signal_ids={"risk"}
    )
    assert not_fully_approved["acme.delivery"].risk_level == RiskLevel.LOW
    assert [ref.canonical_id for ref in not_fully_approved["acme.delivery"].source_refs] == [
        "synthetic-native-id"
    ]

    replay = run_sharepoint_evidence_stage(
        program_id="acme",
        programs_root=tmp_path,
        as_of=_AS_OF + timedelta(days=1),
        workstreams=workstreams,
    )
    assert (replay.lanes_updated, replay.evidence_written, replay.gaps) == (2, 0, ())
    assert len(load_evidence_records("acme", programs_root=tmp_path)) == 3


def test_failed_lane_is_reported_without_discarding_another_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _write_signals(
        tmp_path,
        _signal("bad", "Risk: blocker", workstream_id="acme.bad"),
        _signal("good", "Risk: normal", workstream_id="acme.good"),
    )
    persist = evidence_extraction_stage.persist_evidence

    def fail_one_lane(
        evidence: WorkstreamEvidence,
        *,
        program_id: str,
        programs_root: Path,
        backing_signal_ids: tuple[str, ...] = (),
    ) -> bool:
        if evidence.lane_id == "acme.bad":
            raise OSError("synthetic write failure")
        return persist(
            evidence,
            program_id=program_id,
            programs_root=programs_root,
            backing_signal_ids=backing_signal_ids,
        )

    monkeypatch.setattr(evidence_extraction_stage, "persist_evidence", fail_one_lane)
    result = run_sharepoint_evidence_stage(program_id="acme", programs_root=tmp_path, as_of=_AS_OF)

    assert result.lanes_updated == 1
    assert result.evidence_written == 1
    assert len(result.gaps) == 1
    assert "acme.bad" in result.gaps[0]
    assert "SharePoint evidence stage error" in caplog.text
    assert [record.evidence.lane_id for record in load_evidence_records("acme", programs_root=tmp_path)] == [
        "acme.good"
    ]


@pytest.mark.parametrize(
    ("markers", "expected"),
    [
        ([], RiskLevel.UNKNOWN),
        (["Risk: schedule change"], RiskLevel.MEDIUM),
        (["Risk: BLOCKER on deployment"], RiskLevel.HIGH),
        (["Risk: critical issue"], RiskLevel.HIGH),
        (["Risk: escalate immediately"], RiskLevel.HIGH),
    ],
)
def test_risk_markers_map_to_existing_levels(markers: list[str], expected: RiskLevel) -> None:
    assert _derive_risk_level(markers) == expected


@pytest.mark.parametrize(
    ("text", "expected_status"),
    [
        ("Milestone: launch, 2026-06-30 owner:ava", "open"),
        ("Milestone: launch complete, 2026-06-30 owner:ava", "closed"),
        ("Milestone: launch blocked, 2026-06-30 owner:ava", "missed"),
    ],
)
def test_milestone_dates_and_status_are_extracted(text: str, expected_status: str) -> None:
    eta = _parse_eta_from_milestone(text)
    assert eta is not None
    assert eta.eta_date == date(2026, 6, 30)
    assert eta.owner == "ava"
    assert eta.status == expected_status


@pytest.mark.parametrize("text", ["Milestone: no date", "Milestone: invalid 2026-02-30"])
def test_missing_or_invalid_milestone_date_is_not_an_eta(text: str) -> None:
    assert _parse_eta_from_milestone(text) is None


@pytest.mark.parametrize(
    ("text", "explicit_lane", "expected"),
    [
        ("Risk: DEPLOYMENT is blocked", None, "acme.delivery"),
        ("Risk: deploy is blocked", None, "acme.delivery"),
        ("Risk: redeployment is blocked", None, "acme"),
        ("Risk: deployment is blocked", "acme.explicit", "acme.explicit"),
        ("Risk: deployment is blocked", "acme", "acme.delivery"),
    ],
)
def test_workstream_routing_prefers_explicit_lane_then_whole_word_alias(
    text: str, explicit_lane: str | None, expected: str
) -> None:
    workstreams = (SimpleNamespace(id="acme.delivery", name="Deployment", aliases=("deploy",)),)
    keywords = _build_workstream_keyword_map(workstreams)
    signal = _deserialize_signal(_signal("s", text, workstream_id=explicit_lane))

    assert _route_signal_to_lane(signal, keywords, "acme") == expected
