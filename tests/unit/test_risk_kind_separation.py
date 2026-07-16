"""Unit tests for ADF-W4.2: risk three-way separation + no hardcoded assessed defaults.

Covers Section 8.10.1 and INV-ADF-13: machine-derived risks are CANDIDATEs
with UNASSESSED probability/impact (never the rejected POSSIBLE/MEDIUM
false-precision defaults), and the ``kind`` field round-trips through the
YAML register so candidates/hygiene don't silently default to strategic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.models_v2 import RiskImpact, RiskKind, RiskProbability
from src.core.risk_register_engine import (
    _parse_risk_kind,
    _parse_risk_entry,
    _risk_entry_to_record,
    compute_risk_score,
    load_risk_register,
    upsert_risk_from_signal,
)


def _write_minimal_register(program_dir: Path, risks: list[dict]) -> None:
    import yaml

    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "risk_register.yaml").write_text(
        yaml.safe_dump({"schema_version": "1.0", "risks": risks}, sort_keys=False),
        encoding="utf-8",
    )


def test_upsert_from_signal_stamps_candidate_not_assessed(tmp_path: Path) -> None:
    program_dir = tmp_path / "xpf"
    # Start with one existing strategic risk (realistic: upsert matches or appends).
    _write_minimal_register(
        program_dir,
        [
            {
                "id": "risk-existing",
                "program_id": "xpf",
                "title": "Existing strategic risk",
                "description": "Already assessed",
                "probability": "likely",
                "impact": "high",
                "category": "technical",
                "owner_alias": "owner",
                "status": "open",
                "identified_date": "2026-07-12",
                "entity_refs": ["99999"],
            }
        ],
    )
    entry = upsert_risk_from_signal(
        "xpf",
        signal_id="sig-1",
        signal_text="Build pipeline flakiness increasing on the release branch",
        signal_entity_refs=("12345",),
        signal_workstream_id="ws-release",
        programs_root=tmp_path,
    )
    assert entry.kind == RiskKind.CANDIDATE.value
    assert entry.probability is RiskProbability.UNASSESSED
    assert entry.impact is RiskImpact.UNASSESSED
    # INV-ADF-13: never the rejected hardcoded assessed defaults.
    assert entry.probability is not RiskProbability.POSSIBLE
    assert entry.impact is not RiskImpact.MEDIUM


def test_compute_risk_score_unassessed_candidate_is_zero() -> None:
    """An unassessed candidate has no score; it must not outrank assessed risks."""
    from src.core.models_v2 import RiskCategory, RiskEntry, RiskStatus
    from datetime import date

    candidate = RiskEntry(
        id="r1", program_id="xpf", title="t", description="d",
        probability=RiskProbability.UNASSESSED, impact=RiskImpact.UNASSESSED,
        category=RiskCategory.SCHEDULE, owner_alias="unassigned",
        mitigation_plan=None, mitigation_due_date=None,
        linked_workstream_ids=(), linked_work_item_ids=(), linked_milestone_ids=(),
        linked_claim_ids=(), linked_action_ids=(), status=RiskStatus.OPEN,
        identified_date=date(2026, 7, 12), identified_in_vertex_issue=None,
        last_reviewed_date=None, entity_refs=(), kind=RiskKind.CANDIDATE.value,
    )
    assert compute_risk_score(candidate) == 0


def test_compute_risk_score_assessed_still_works() -> None:
    from src.core.models_v2 import RiskCategory, RiskEntry, RiskStatus
    from datetime import date

    high = RiskEntry(
        id="r2", program_id="xpf", title="t", description="d",
        probability=RiskProbability.VERY_LIKELY, impact=RiskImpact.CRITICAL,
        category=RiskCategory.SCHEDULE, owner_alias="owner",
        mitigation_plan=None, mitigation_due_date=None,
        linked_workstream_ids=(), linked_work_item_ids=(), linked_milestone_ids=(),
        linked_claim_ids=(), linked_action_ids=(), status=RiskStatus.OPEN,
        identified_date=date(2026, 7, 12), identified_in_vertex_issue=None,
        last_reviewed_date=None, entity_refs=(), kind=RiskKind.STRATEGIC.value,
    )
    # VERY_LIKELY(4) * CRITICAL(4) = 16
    assert compute_risk_score(high) == 16


def test_parse_risk_kind_absent_defaults_to_strategic() -> None:
    assert _parse_risk_kind(None) == RiskKind.STRATEGIC.value
    assert _parse_risk_kind("") == RiskKind.STRATEGIC.value


def test_parse_risk_kind_recognized_values() -> None:
    assert _parse_risk_kind("candidate") == RiskKind.CANDIDATE.value
    assert _parse_risk_kind("strategic") == RiskKind.STRATEGIC.value
    assert _parse_risk_kind("hygiene") == RiskKind.HYGIENE.value
    assert _parse_risk_kind("CANDIDATE") == RiskKind.CANDIDATE.value  # case-insensitive


def test_parse_risk_kind_unknown_passes_through() -> None:
    # Honest degradation: unknown kind loads rather than crashing the register.
    assert _parse_risk_kind("custom-kind") == "custom-kind"


def test_kind_round_trips_through_serialize_parse() -> None:
    from src.core.models_v2 import RiskCategory, RiskEntry, RiskStatus
    from datetime import date

    candidate = RiskEntry(
        id="r3", program_id="xpf", title="t", description="d",
        probability=RiskProbability.UNASSESSED, impact=RiskImpact.UNASSESSED,
        category=RiskCategory.SCHEDULE, owner_alias="unassigned",
        mitigation_plan=None, mitigation_due_date=None,
        linked_workstream_ids=(), linked_work_item_ids=(), linked_milestone_ids=(),
        linked_claim_ids=(), linked_action_ids=(), status=RiskStatus.OPEN,
        identified_date=date(2026, 7, 12), identified_in_vertex_issue=None,
        last_reviewed_date=None, entity_refs=(), kind=RiskKind.CANDIDATE.value,
    )
    record = _risk_entry_to_record(candidate)
    assert record["kind"] == "candidate"
    reparsed = _parse_risk_entry("xpf", record)
    assert reparsed.kind == RiskKind.CANDIDATE.value


def test_load_register_preserves_kind(tmp_path: Path) -> None:
    program_dir = tmp_path / "xpf"
    _write_minimal_register(
        program_dir,
        [
            {
                "id": "risk-a",
                "program_id": "xpf",
                "title": "Candidate risk",
                "description": "Machine-derived",
                "probability": "unassessed",
                "impact": "unassessed",
                "category": "schedule",
                "owner_alias": "unassigned",
                "status": "open",
                "identified_date": "2026-07-12",
                "entity_refs": [],
                "kind": "candidate",
            },
            {
                "id": "risk-b",
                "program_id": "xpf",
                "title": "Strategic risk",
                "description": "Human-assessed",
                "probability": "likely",
                "impact": "high",
                "category": "technical",
                "owner_alias": "owner",
                "status": "open",
                "identified_date": "2026-07-12",
                "entity_refs": [],
                # kind absent -> strategic default
            },
        ],
    )
    entries = load_risk_register("xpf", programs_root=tmp_path)
    by_id = {e.id: e for e in entries}
    assert by_id["risk-a"].kind == RiskKind.CANDIDATE.value
    assert by_id["risk-a"].probability is RiskProbability.UNASSESSED
    assert by_id["risk-b"].kind == RiskKind.STRATEGIC.value
    assert by_id["risk-b"].probability is RiskProbability.LIKELY


def test_risk_kind_enum_has_three_values() -> None:
    assert {RiskKind.STRATEGIC, RiskKind.CANDIDATE, RiskKind.HYGIENE} == set(RiskKind)


def test_unassessed_probability_impact_enums_exist() -> None:
    assert RiskProbability.UNASSESSED.value == "unassessed"
    assert RiskImpact.UNASSESSED.value == "unassessed"
