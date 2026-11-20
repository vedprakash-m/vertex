from __future__ import annotations

import dataclasses

import pytest

from src.core.claim_evidence_map import (
    ClaimEvidenceLink,
    ClaimEvidenceReport,
    FindingKind,
    TruthConsistencyMode,
    build_claim_evidence_map,
    evaluate_truth_consistency,
)


def _link(claim_id: str, *refs: str, text: str = "claim") -> ClaimEvidenceLink:
    return ClaimEvidenceLink(claim_id=claim_id, claim_text=text, evidence_refs=refs)


def test_value_objects_are_frozen() -> None:
    link = _link("c1", "ado:1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        link.claim_id = "c2"  # type: ignore[misc]
    report = build_claim_evidence_map([link], {"ado:1"})
    assert isinstance(report, ClaimEvidenceReport)
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.findings = ()  # type: ignore[misc]


def test_claim_with_known_evidence_is_supported() -> None:
    report = build_claim_evidence_map([_link("c1", "ado:1")], {"ado:1"})
    assert [link.claim_id for link in report.supported] == ["c1"]
    assert report.unsupported == ()
    assert report.findings == ()


def test_claim_without_evidence_is_unsupported() -> None:
    report = build_claim_evidence_map([_link("c1")], {"ado:1"})
    assert report.supported == ()
    assert [link.claim_id for link in report.unsupported] == ["c1"]
    assert [(f.kind, f.claim_id) for f in report.findings] == [
        (FindingKind.UNSUPPORTED_CLAIM, "c1")
    ]


def test_claim_with_only_dangling_refs_is_unsupported_with_both_findings() -> None:
    report = build_claim_evidence_map([_link("c1", "ado:9")], {"ado:1"})
    assert [link.claim_id for link in report.unsupported] == ["c1"]
    assert [(f.kind, f.evidence_ref) for f in report.findings] == [
        (FindingKind.DANGLING_EVIDENCE_REF, "ado:9"),
        (FindingKind.UNSUPPORTED_CLAIM, None),
    ]


def test_mixed_refs_supported_but_dangling_ref_reported() -> None:
    report = build_claim_evidence_map([_link("c1", "ado:1", "ado:9")], {"ado:1"})
    assert [link.claim_id for link in report.supported] == ["c1"]
    assert [(f.kind, f.claim_id, f.evidence_ref) for f in report.findings] == [
        (FindingKind.DANGLING_EVIDENCE_REF, "c1", "ado:9")
    ]


def test_blank_refs_do_not_count_as_evidence() -> None:
    report = build_claim_evidence_map([_link("c1", "", "   ")], {""})
    assert [link.claim_id for link in report.unsupported] == ["c1"]
    assert [f.kind for f in report.findings] == [FindingKind.UNSUPPORTED_CLAIM]


def test_duplicate_dangling_refs_reported_once_per_claim() -> None:
    report = build_claim_evidence_map([_link("c1", "x", "x")], set())
    dangling = [f for f in report.findings if f.kind is FindingKind.DANGLING_EVIDENCE_REF]
    assert len(dangling) == 1


def test_output_is_deterministic_and_preserves_claim_order() -> None:
    claims = [_link("b", "r2"), _link("a"), _link("c", "r1")]
    first = build_claim_evidence_map(claims, {"r1", "r2"})
    second = build_claim_evidence_map(tuple(claims), frozenset({"r2", "r1"}))
    assert first == second
    assert [link.claim_id for link in first.supported] == ["b", "c"]


def test_evaluate_observe_only_reports_would_block_without_raising() -> None:
    result = evaluate_truth_consistency([_link("c1"), _link("c2", "ado:1")], {"ado:1"})
    assert result.mode is TruthConsistencyMode.OBSERVE_ONLY
    assert result.would_block is True
    assert result.blocked is False
    assert [f.kind for f in result.findings] == [FindingKind.UNSUPPORTED_CLAIM]


def test_evaluate_release_block_blocks_unsupported_claims() -> None:
    result = evaluate_truth_consistency(
        [_link("c1")],
        {"ado:1"},
        mode=TruthConsistencyMode.RELEASE_BLOCK,
    )
    assert result.would_block is True
    assert result.blocked is True


def test_evaluate_dangling_ref_would_block_release() -> None:
    result = evaluate_truth_consistency([_link("c1", "ado:1", "ado:9")], {"ado:1"})
    assert result.would_block is True
    assert result.blocked is False
    assert [f.kind for f in result.findings] == [FindingKind.DANGLING_EVIDENCE_REF]

    release = evaluate_truth_consistency(
        [_link("c1", "ado:1", "ado:9")],
        {"ado:1"},
        mode=TruthConsistencyMode.RELEASE_BLOCK,
    )
    assert release.blocked is True


def test_evaluate_clean_and_empty_inputs() -> None:
    clean = evaluate_truth_consistency([_link("c1", "ado:1")], {"ado:1"})
    empty = evaluate_truth_consistency([], set())
    assert clean.would_block is False and clean.findings == ()
    assert empty.would_block is False and empty.findings == ()


def test_enum_values_are_json_portable_strings() -> None:
    assert FindingKind.UNSUPPORTED_CLAIM == "unsupported_claim"
    assert FindingKind.DANGLING_EVIDENCE_REF == "dangling_evidence_ref"
    assert TruthConsistencyMode.OBSERVE_ONLY == "observe_only"
    assert TruthConsistencyMode.RELEASE_BLOCK == "release_block"
